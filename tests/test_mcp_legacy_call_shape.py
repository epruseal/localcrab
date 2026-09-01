"""
RED-phase contract tests for issue #251 -- legacy ``tools/call`` shape
validation aligned with the modern validator.

Design of record: /home/asdf/orch-scratch/o251/design-v7.md.

Issue #136 (PR #243) deliberately left the legacy era's call-shape checks as
a bare ``if not name`` truthiness test and an unconditional
``params.get("arguments") or {}`` coercion, pinning that behaviour in
``test_mcp_protocol_2026.py::test_legacy_tools_call_non_string_name_unchanged``
as "R4 strictness is modern-only". Issue #251 explicitly hands the follow-up
back: "legacy 경로의 name/arguments 형상 미검증은 기존 계약 유지 결정이었다 --
D절 이행 시 modern 검증기와 정합시킨다." This file is that alignment's
contract; the #243 pin is replaced (not deleted) in its own file.

The safety rule the alignment obeys, and which every pin here encodes:
**no input that currently makes a tool RUN changes behaviour.** Measured on
the pre-change code, the shapes rejected below never reached tool execution
(a non-string name dead-ends in an unhashable registry lookup or an unknown
tool; a truthy non-object ``arguments`` dies on ``**`` unpacking). The one
malformed-looking shape that DOES run a tool today -- a present but *falsy*
non-object ``arguments`` (``null``/``[]``/``0``/``0.0``/``false``/``""``),
which ``or {}`` normalises to ``{}`` -- is therefore preserved as a
compatibility exception and pinned as such, not aligned.

At the point this file is committed the Error/Edge groups are expected to
FAIL; the Normal group passes before and after and is a regression pin whose
detection power is demonstrated by reverse mutation (dropping the falsy
carve-out), not by a RED.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from opencrab.auth import Principal, bootstrap_local_user, principal_scope
from opencrab.mcp.http_app import create_app
from opencrab.stores.sql_store import SQLStore

INVALID_PARAMS = -32602

NAME_FAULT = "Invalid params: 'name' must be a non-empty string in tools/call params."
ARGS_FAULT = "Invalid params: 'arguments' must be an object when present"
PARAMS_FAULT = "Invalid params: params must be an object in tools/call."

# `or {}` normalises exactly these to {} -- the compatibility exception's
# full extent, enumerated rather than sampled so a narrowed carve-out fails.
FALSY_NON_OBJECTS = [None, [], 0, 0.0, False, ""]

# A registered tool the LOCAL principal may call, and an ADMIN-tier one a
# REMOTE principal may not (its name must stay indistinguishable from an
# unregistered one -- #150).
VISIBLE_TOOL = "tool_search"
ADMIN_TOOL = "harness_promotion_apply"
UNREGISTERED_TOOL = "no_such_tool_251"

LOCAL = Principal(user_id="u-local-251", is_local=True, disabled=False)
REMOTE = Principal(user_id="u-remote-251", is_local=False, disabled=False)


@pytest.fixture
def server():
    """Dual-era server (default configuration)."""
    from opencrab.mcp.server import MCPServer

    with patch("opencrab.mcp.server.get_settings") as mock_cfg:
        mock_cfg.return_value = MagicMock(
            mcp_server_name="opencrab-test",
            mcp_server_version="0.0.1",
            mcp_protocol_versions=None,
        )
        return MCPServer()


def _call(server, params, req_id=1):
    body = {"jsonrpc": "2.0", "method": "tools/call", "params": params}
    if req_id is not None:
        body["id"] = req_id
    return server.handle_request(body)


# ---------------------------------------------------------------------------
# Normal -- everything that runs a tool today keeps running it
# ---------------------------------------------------------------------------


class TestLegacyCallShapeNormal:
    def test_valid_call_envelope_stays_legacy_shaped(self, server):
        """Byte-compat pin: aligning the shape checks must not leak modern
        envelope fields into the legacy result."""
        with patch("opencrab.mcp.server.dispatch_tool", return_value={"ok": True}) as dispatch:
            response = _call(server, {"name": VISIBLE_TOOL, "arguments": {"q": "x"}})
        dispatch.assert_called_once_with(VISIBLE_TOOL, {"q": "x"})
        result = response["result"]
        assert "resultType" not in result
        assert "isError" not in result
        assert "_meta" not in result
        assert json.loads(result["content"][0]["text"]) == {"ok": True}

    def test_absent_arguments_still_dispatches_empty_dict(self, server):
        with patch("opencrab.mcp.server.dispatch_tool", return_value={"ok": True}) as dispatch:
            response = _call(server, {"name": VISIBLE_TOOL})
        dispatch.assert_called_once_with(VISIBLE_TOOL, {})
        assert "error" not in response

    @pytest.mark.parametrize("arguments", FALSY_NON_OBJECTS)
    def test_falsy_non_object_arguments_still_run_the_tool(self, server, arguments):
        """COMPATIBILITY EXCEPTION pin (#251 §2).

        A present but falsy non-object ``arguments`` is coerced to ``{}`` by
        the historical ``or {}`` and the tool RUNS today. That is the one
        malformed-looking shape a real client could be relying on (an
        optional field serialised as JSON null is the obvious case), so it is
        preserved rather than aligned -- rejecting it would break a call that
        works. Green before AND after the change: its detection power is the
        reverse mutation (drop the falsy carve-out and every case here
        turns into -32602), not a RED.
        """
        with patch("opencrab.mcp.server.dispatch_tool", return_value={"ok": True}) as dispatch:
            response = _call(server, {"name": VISIBLE_TOOL, "arguments": arguments})
        dispatch.assert_called_once_with(VISIBLE_TOOL, {})
        assert "error" not in response
        assert json.loads(response["result"]["content"][0]["text"]) == {"ok": True}


# ---------------------------------------------------------------------------
# Error -- shapes that never ran a tool now stop before dispatch
# ---------------------------------------------------------------------------


class TestLegacyCallShapeErrors:
    @pytest.mark.parametrize("name", [None, "", 0, 0.0, False, [], {}])
    def test_falsy_name_keeps_invalid_params_with_the_shared_message(self, server, name):
        """These were already -32602 (``if not name`` fires regardless of
        type); only the message changes, to the shared validator's."""
        with patch("opencrab.mcp.server.dispatch_tool") as dispatch:
            response = _call(server, {"name": name, "arguments": {}})
        assert response["error"]["code"] == INVALID_PARAMS
        assert response["error"]["message"] == NAME_FAULT
        dispatch.assert_not_called()

    def test_absent_name_keeps_invalid_params_with_the_shared_message(self, server):
        with patch("opencrab.mcp.server.dispatch_tool") as dispatch:
            response = _call(server, {"arguments": {}})
        assert response["error"]["code"] == INVALID_PARAMS
        assert response["error"]["message"] == NAME_FAULT
        dispatch.assert_not_called()

    @pytest.mark.parametrize("name", [123, True, ["a"], {"a": 1}])
    def test_truthy_non_string_name_is_invalid_params_before_dispatch(self, server, name):
        """Was -32601 (hashable: unknown tool) or a success envelope carrying
        ``unhashable type`` (unhashable). Neither ever ran a tool."""
        with patch("opencrab.mcp.server.dispatch_tool") as dispatch:
            response = _call(server, {"name": name, "arguments": {}})
        assert response["error"]["code"] == INVALID_PARAMS
        assert response["error"]["message"] == NAME_FAULT
        dispatch.assert_not_called()

    @pytest.mark.parametrize("arguments", [[1, 2], "str", 5, 1.5, "tenant_id", True])
    def test_truthy_non_object_arguments_is_invalid_params_before_dispatch(
        self, server, arguments
    ):
        """``**`` unpacking of a non-mapping never reached a tool body; the
        ``"tenant_id"`` case additionally mis-fired ``_FORBIDDEN_ARGS``'s
        substring check, which is now unreachable."""
        with patch("opencrab.mcp.server.dispatch_tool") as dispatch:
            response = _call(server, {"name": VISIBLE_TOOL, "arguments": arguments})
        assert response["error"]["code"] == INVALID_PARAMS
        assert response["error"]["message"] == ARGS_FAULT
        dispatch.assert_not_called()

    @pytest.mark.parametrize("params", [[1], "s", 5, True])
    def test_truthy_non_object_params_is_invalid_params_not_internal_error(
        self, server, params
    ):
        """Was -32603 plus an ERROR stack trace: a caller shape fault
        mis-reported as a server fault."""
        with patch("opencrab.mcp.server.dispatch_tool") as dispatch:
            response = _call(server, params)
        assert response["error"]["code"] == INVALID_PARAMS
        assert response["error"]["message"] == PARAMS_FAULT
        dispatch.assert_not_called()

    @pytest.mark.parametrize("params", FALSY_NON_OBJECTS)
    def test_falsy_params_are_normalised_before_the_object_guard(self, server, params):
        """``handle_request``'s own ``request.get("params") or {}`` turns
        these into ``{}`` BEFORE the handler runs, so they surface as an
        absent name -- not as the params-object fault. Pinned so the new
        guard is never mistaken for the thing that handles them."""
        with patch("opencrab.mcp.server.dispatch_tool") as dispatch:
            response = _call(server, params)
        assert response["error"]["code"] == INVALID_PARAMS
        assert response["error"]["message"] == NAME_FAULT
        dispatch.assert_not_called()


# ---------------------------------------------------------------------------
# Edge
# ---------------------------------------------------------------------------


class TestLegacyCallShapeEdge:
    def test_unhashable_name_no_longer_leaks_a_success_envelope(self, server):
        """The sharpest symptom: a dict/list name made the registry lookup
        raise ``unhashable type``, which the generic handler wrapped into a
        JSON-RPC *result*. A malformed request must not come back as a
        success."""
        with principal_scope(LOCAL):
            response = _call(server, {"name": {"a": 1}, "arguments": {}})
        assert "result" not in response
        assert response["error"]["code"] == INVALID_PARAMS
        assert "unhashable" not in json.dumps(response)

    def test_registration_state_is_not_an_oracle_for_malformed_arguments(self, server):
        """#150 keeps "hidden tool" and "unregistered tool" indistinguishable
        (both -32601) before and after this change. What the change ADDS is
        that a *registered and permitted* name stops being distinguishable
        too: today malformed ``arguments`` returns a success envelope for it
        and -32601 for the other two, which answers "is this a tool I may
        call?". Rejecting the shape before dispatch removes that oracle.

        Real dispatch (no patch) on purpose -- patching it would erase the
        very ordering under test. No store is touched: every case fails
        before a tool body runs.
        """
        malformed = {"arguments": [1, 2]}
        with principal_scope(LOCAL):
            visible = _call(server, {"name": VISIBLE_TOOL, **malformed})
            unregistered = _call(server, {"name": UNREGISTERED_TOOL, **malformed})
        with principal_scope(REMOTE):
            hidden = _call(server, {"name": ADMIN_TOOL, **malformed})
        seen = {
            (r["error"]["code"], r["error"]["message"])
            for r in (visible, unregistered, hidden)
        }
        assert seen == {(INVALID_PARAMS, ARGS_FAULT)}

    @pytest.mark.parametrize(
        "params",
        [
            {"name": ["a"], "arguments": {}},
            {"name": VISIBLE_TOOL, "arguments": "str"},
            [1],
        ],
    )
    def test_malformed_notification_stays_silent_but_never_dispatches(self, server, params):
        """A notification is still never answered (its 202 at the HTTP layer
        is unchanged); what changes is that the malformed shape no longer
        reaches ``dispatch_tool``."""
        with patch("opencrab.mcp.server.dispatch_tool") as dispatch:
            response = _call(server, params, req_id=None)
        assert response is None
        dispatch.assert_not_called()

    @pytest.mark.parametrize(
        "params",
        [
            {"name": 123, "arguments": {}},
            {"name": VISIBLE_TOOL, "arguments": [1, 2]},
        ],
    )
    def test_unbound_principal_malformed_shape_does_not_leak_lookup_error(
        self, server, params
    ):
        """Not reachable through any transport (HTTP ``_check`` and the stdio
        entry points both bind a principal first), but a direct call used to
        let ``current_principal()``'s LookupError -- a raw ContextVar repr --
        travel back inside a success envelope. Shape validation now runs
        first, so nothing internal escapes.
        """
        response = _call(server, params)
        assert response["error"]["code"] == INVALID_PARAMS
        assert "ContextVar" not in json.dumps(response)
        assert "result" not in response

    def test_shared_validator_default_still_rejects_falsy_arguments(self):
        """The compatibility exception lives in the legacy call site, NOT in
        the shared validator: called plainly it must stay strict, or the
        modern era would silently inherit the carve-out."""
        from opencrab.mcp import protocol

        for arguments in FALSY_NON_OBJECTS:
            fault = protocol.validate_tools_call_params(
                {"name": VISIBLE_TOOL, "arguments": arguments}
            )
            assert fault is not None, arguments
            assert fault.code == INVALID_PARAMS


# ---------------------------------------------------------------------------
# HTTP transport -- statuses must not move (#136/#250 contracts)
# ---------------------------------------------------------------------------


@pytest.fixture
def bootstrapped(tmp_path):
    sql = SQLStore(f"sqlite:///{tmp_path}/opencrab.db")
    user_id, secret = bootstrap_local_user(sql)
    return sql, user_id, secret


def _stub_context(sql):
    return {
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


def _client_for_versions(bootstrapped, tmp_path, monkeypatch, versions):
    """Fixtures duplicated from tests/test_http_app_modern_only.py by the
    same policy that file states: each file owns its axis independently."""
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
def client_dual(bootstrapped, tmp_path, monkeypatch):
    yield from _client_for_versions(bootstrapped, tmp_path, monkeypatch, None)


@pytest.fixture
def client_modern_only(bootstrapped, tmp_path, monkeypatch):
    yield from _client_for_versions(bootstrapped, tmp_path, monkeypatch, "2026-07-28")


def _auth(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


def _legacy_call(name, arguments, req_id=1):
    body = {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    if req_id is not None:
        body["id"] = req_id
    return body


class TestLegacyCallShapeOverHttp:
    def test_dual_single_malformed_request_stays_200_with_invalid_params(
        self, client_dual, bootstrapped
    ):
        """A dual-era server answers legacy traffic with a blanket 200; the
        modern status mapping is gated on ``modern_http or not
        legacy_enabled`` and must not start applying here just because the
        envelope changed."""
        _, _, secret = bootstrapped
        resp = client_dual.post("/mcp", json=_legacy_call(VISIBLE_TOOL, [1, 2]), headers=_auth(secret))
        assert resp.status_code == 200
        assert resp.json()["error"]["code"] == INVALID_PARAMS
        assert resp.json()["error"]["message"] == ARGS_FAULT

    def test_dual_batch_malformed_element_does_not_disturb_its_siblings(
        self, client_dual, bootstrapped
    ):
        """Batch arrays are a legacy-only extension whose elements each go
        through ``handle_request``: only the malformed element's envelope may
        change, and the array status stays 200."""
        _, _, secret = bootstrapped
        with patch("opencrab.mcp.server.dispatch_tool", return_value={"ok": True}):
            resp = client_dual.post(
                "/mcp",
                json=[
                    _legacy_call(VISIBLE_TOOL, {}, req_id=1),
                    _legacy_call(VISIBLE_TOOL, [1, 2], req_id=2),
                ],
                headers=_auth(secret),
            )
        assert resp.status_code == 200
        first, second = resp.json()
        assert first["id"] == 1 and "error" not in first
        assert json.loads(first["result"]["content"][0]["text"]) == {"ok": True}
        assert second["id"] == 2
        assert second["error"]["code"] == INVALID_PARAMS
        assert second["error"]["message"] == ARGS_FAULT

    def test_dual_batch_malformed_notification_element_is_dropped_undispatched(
        self, client_dual, bootstrapped
    ):
        _, _, secret = bootstrapped
        with patch("opencrab.mcp.server.dispatch_tool", return_value={"ok": True}) as dispatch:
            resp = client_dual.post(
                "/mcp",
                json=[
                    _legacy_call(["a"], {}, req_id=None),
                    _legacy_call(VISIBLE_TOOL, {}, req_id=7),
                ],
                headers=_auth(secret),
            )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1 and body[0]["id"] == 7
        dispatch.assert_called_once_with(VISIBLE_TOOL, {})

    def test_dual_all_notification_batch_still_202(self, client_dual, bootstrapped):
        _, _, secret = bootstrapped
        with patch("opencrab.mcp.server.dispatch_tool") as dispatch:
            resp = client_dual.post(
                "/mcp",
                json=[_legacy_call(["a"], {}, req_id=None), _legacy_call(VISIBLE_TOOL, "str", req_id=None)],
                headers=_auth(secret),
            )
        assert resp.status_code == 202
        assert resp.content == b""
        dispatch.assert_not_called()

    def test_modern_only_legacy_call_rejection_unchanged(
        self, client_modern_only, bootstrapped
    ):
        """#250's contract: on a modern-only server the legacy-disabled gate
        fires before the handler, so this change cannot reach it."""
        _, _, secret = bootstrapped
        resp = client_modern_only.post(
            "/mcp", json=_legacy_call(VISIBLE_TOOL, [1, 2]), headers=_auth(secret)
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == INVALID_PARAMS
        assert "Protocol version metadata is required" in resp.json()["error"]["message"]
