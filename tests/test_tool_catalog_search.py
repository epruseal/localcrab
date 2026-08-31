"""#135: contract tests for the tool catalog snapshot and the `tool_search` tool.

Covers `opencrab.mcp.tools._registry.get_tool_catalog` (principal-scoped
snapshot + fingerprint) and the `tool_search` bootstrap discovery surface.
Principals are plain in-memory objects bound via ``principal_scope`` (the
same pattern as tests/test_mcp_tool_exposure.py) -- no store round trip.

The query="pack" ordering test is a description-snapshot contract: it pins
the exact match order against the tool descriptions as they exist today.
Changing a description that adds/removes the substring "pack" must update
that literal alongside.
"""

from __future__ import annotations

import dataclasses
import json
from unittest.mock import MagicMock, patch

import pytest

from opencrab.auth import Principal, current_principal, principal_scope
from opencrab.mcp.tools import TOOLS, dispatch_tool, tools_for_principal
from opencrab.mcp.tools._registry import _REGISTRY, AccessTier, ToolSpec

_LOCAL = Principal(user_id="local-1", is_local=True, disabled=False)
_REMOTE = Principal(user_id="remote-1", is_local=False, disabled=False)

_ADMIN_TOOL_NAMES = {"schema_pack_install", "schema_pack_uninstall", "harness_promotion_apply"}

# Base-measured literal (design v4 §4.4): substring "pack", local view.
# Name-match group first (catalog order), then description-only group
# (catalog order). tool_search's own description deliberately avoids "pack".
_PACK_QUERY_EXPECTED = [
    "content_pack_list",
    "schema_pack_list",
    "schema_pack_install",
    "schema_pack_uninstall",
    "pack_create",
    "pack_ingest",
    "pack_publish",
    "pack_fork",
    "ontology_list_nodes",
    "ontology_list_edges",
    "harness_promotion_apply",
]


def get_tool_catalog(principal: Principal):
    """Late import: the symbol lands with the implementation commit (#135);
    resolving it at call time keeps the RED state an assertion/ImportError
    inside tests rather than a collection error."""
    from opencrab.mcp.tools._registry import get_tool_catalog as impl

    return impl(principal)


def _search(principal: Principal, **arguments):
    with principal_scope(principal):
        return dispatch_tool("tool_search", arguments)


def _make_server():
    with patch("opencrab.mcp.server.get_settings") as cfg:
        cfg.return_value = MagicMock(
            mcp_server_name="catalog-test",
            mcp_server_version="0",
            mcp_protocol_versions=None,
        )
        from opencrab.mcp.server import MCPServer

        return MCPServer()


def _legacy_call(server, arguments):
    return server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "tool_search", "arguments": arguments},
        }
    )


class TestToolSearchNormal:
    def test_empty_query_returns_whole_visible_catalog_in_tools_list_order(self):
        result = _search(_LOCAL, query="")
        names = [t["name"] for t in result["tools"]]
        with principal_scope(_LOCAL):
            visible = [t["name"] for t in tools_for_principal(current_principal())]
        assert names == visible
        assert result["total_matched"] == result["returned"] == len(visible)

    def test_query_omitted_equals_empty_query(self):
        assert [t["name"] for t in _search(_LOCAL)["tools"]] == [
            t["name"] for t in _search(_LOCAL, query="")["tools"]
        ]

    def test_pack_query_group_order_literal_snapshot(self):
        result = _search(_LOCAL, query="pack")
        assert [t["name"] for t in result["tools"]] == _PACK_QUERY_EXPECTED

    def test_query_matching_is_case_insensitive(self):
        assert [t["name"] for t in _search(_LOCAL, query="PACK")["tools"]] == (
            _PACK_QUERY_EXPECTED
        )

    def test_include_schema_true_embeds_input_schema(self):
        result = _search(_LOCAL, query="tool_search", include_schema=True)
        (entry,) = [t for t in result["tools"] if t["name"] == "tool_search"]
        assert isinstance(entry["inputSchema"], dict)
        assert entry["inputSchema"].get("type") == "object"

    def test_include_schema_false_and_omitted_have_no_schema_key(self):
        for args in ({"include_schema": False}, {}):
            result = _search(_LOCAL, query="tool_search", **args)
            for entry in result["tools"]:
                assert "inputSchema" not in entry

    def test_catalog_version_stable_across_calls_and_generated_at_present(self):
        first = _search(_LOCAL)
        second = _search(_LOCAL)
        assert first["catalog_version"] == second["catalog_version"]
        assert isinstance(first["generated_at"], str) and first["generated_at"]

    def test_access_admin_filter_returns_exactly_the_admin_tools_for_local(self):
        result = _search(_LOCAL, access="admin")
        assert {t["name"] for t in result["tools"]} == _ADMIN_TOOL_NAMES
        assert all(t["access"] == "admin" for t in result["tools"])

    def test_legacy_tools_call_roundtrip(self):
        server = _make_server()
        with principal_scope(_LOCAL):
            resp = _legacy_call(server, {"query": "tool_search"})
        payload = json.loads(resp["result"]["content"][0]["text"])
        assert "error" not in payload
        assert payload["catalog_version"]
        assert "tool_search" in [t["name"] for t in payload["tools"]]

    def test_modern_tools_call_roundtrip_is_not_error(self):
        server = _make_server()
        with principal_scope(_LOCAL):
            resp = server.handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "_meta": {
                            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                            "io.modelcontextprotocol/clientCapabilities": {},
                        },
                        "name": "tool_search",
                        "arguments": {"query": "tool_search"},
                    },
                }
            )
        assert resp["result"]["isError"] is False
        payload = json.loads(resp["result"]["content"][0]["text"])
        assert payload["returned"] >= 1

    def test_public_schema_declares_null_alongside_each_input_type(self):
        entry = next(t for t in TOOLS if t["name"] == "tool_search")
        props = entry["inputSchema"]["properties"]
        assert set(props) == {"query", "access", "include_schema", "limit"}
        expected = {
            "query": "string",
            "access": "string",
            "include_schema": "boolean",
            "limit": "integer",
        }
        for key, base_type in expected.items():
            declared = props[key]["type"]
            assert isinstance(declared, list) and set(declared) == {base_type, "null"}, (
                f"{key}: public schema must allow explicit null (got {declared!r})"
            )

    def test_result_carries_all_contract_fields(self):
        result = _search(_LOCAL, query="tool_search")
        for field in ("catalog_version", "generated_at", "total_matched", "returned", "tools", "note"):
            assert field in result
        entry = result["tools"][0]
        assert set(entry) == {"name", "description", "access", "requires_write_lock"}


class TestToolSearchErrors:
    """Invalid non-null inputs surface as the legacy {"error": ...} envelope
    (the server wraps the handler's ValueError) -- representative error path;
    the modern path shares dispatch and is covered by isError elsewhere."""

    @pytest.mark.parametrize(
        "arguments",
        [
            {"limit": 0},
            {"limit": -1},
            {"limit": True},
            {"limit": "5"},
            {"limit": 1.5},
            {"access": "root"},
            {"access": 1},
            {"query": 1},
            {"include_schema": "false"},
            {"include_schema": 0},
        ],
    )
    def test_invalid_argument_returns_error_envelope(self, arguments):
        server = _make_server()
        with principal_scope(_LOCAL):
            resp = _legacy_call(server, arguments)
        payload = json.loads(resp["result"]["content"][0]["text"])
        assert set(payload) == {"error"}, payload


    def test_invalid_argument_on_modern_call_sets_is_error(self):
        """수정 설계 2 B-1: modern 경로 실측 -- tool_search 의 입력 검증 실패가
        modern 봉투에서 isError=true 와 {"error": ...} payload 로 표면화된다."""
        server = _make_server()
        with principal_scope(_LOCAL):
            resp = server.handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "_meta": {
                            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                            "io.modelcontextprotocol/clientCapabilities": {},
                        },
                        "name": "tool_search",
                        "arguments": {"limit": 0},
                    },
                }
            )
        assert resp["result"]["isError"] is True
        payload = json.loads(resp["result"]["content"][0]["text"])
        assert set(payload) == {"error"}


class TestToolSearchNullEquivalence:
    """#135 design [R2-1]: explicit JSON null == omitted, for every optional
    parameter (sibling-handler convention: `x: T | None = None`)."""

    def test_null_limit_means_unbounded(self):
        assert _search(_LOCAL, limit=None)["returned"] == _search(_LOCAL)["returned"]

    def test_null_access_means_no_filter(self):
        assert [t["name"] for t in _search(_LOCAL, access=None)["tools"]] == [
            t["name"] for t in _search(_LOCAL)["tools"]
        ]

    def test_null_query_means_whole_catalog(self):
        assert [t["name"] for t in _search(_LOCAL, query=None)["tools"]] == [
            t["name"] for t in _search(_LOCAL)["tools"]
        ]

    def test_null_include_schema_means_no_schema(self):
        for entry in _search(_LOCAL, include_schema=None)["tools"]:
            assert "inputSchema" not in entry


class TestToolSearchVisibility:
    def test_remote_search_never_returns_admin_tools(self):
        result = _search(_REMOTE)
        names = {t["name"] for t in result["tools"]}
        assert names.isdisjoint(_ADMIN_TOOL_NAMES)
        with principal_scope(_REMOTE):
            visible = {t["name"] for t in tools_for_principal(current_principal())}
        assert names == visible

    def test_remote_admin_filter_is_empty(self):
        result = _search(_REMOTE, access="admin")
        assert result["tools"] == []
        assert result["total_matched"] == 0

    def test_hidden_name_query_indistinguishable_from_unregistered_name(self):
        hidden = _search(_REMOTE, query="schema_pack_install")
        unregistered = _search(_REMOTE, query="no_such_tool_xyz")
        assert hidden["tools"] == unregistered["tools"] == []
        assert hidden["catalog_version"] == unregistered["catalog_version"]

    def test_probe_registration_changes_local_fingerprint_not_remote(self):
        before_local = get_tool_catalog(_LOCAL)["fingerprint"]
        before_remote = get_tool_catalog(_REMOTE)["fingerprint"]
        probe = ToolSpec(
            name="zz_probe_hidden_tool",
            schema={
                "description": "probe",
                "inputSchema": {"type": "object", "properties": {}, "required": []},
            },
            fn=lambda: {},
            order=900,
            access=AccessTier.ADMIN,
            writes=False,
        )
        _REGISTRY[probe.name] = probe
        try:
            assert get_tool_catalog(_REMOTE)["fingerprint"] == before_remote
            assert get_tool_catalog(_LOCAL)["fingerprint"] != before_local
        finally:
            del _REGISTRY[probe.name]
        assert get_tool_catalog(_LOCAL)["fingerprint"] == before_local
        assert get_tool_catalog(_REMOTE)["fingerprint"] == before_remote

    def test_hidden_admin_mutation_changes_local_fingerprint_not_remote(self):
        """[R2-2]: mutating an EXISTING hidden tool's description/schema/lock
        flag (tier unchanged -- still hidden from remote) must not move the
        remote fingerprint, while the local one must move."""
        name = "schema_pack_install"
        original = _REGISTRY[name]
        before_local = get_tool_catalog(_LOCAL)["fingerprint"]
        before_remote = get_tool_catalog(_REMOTE)["fingerprint"]
        mutated = dataclasses.replace(
            original,
            schema={
                "description": "mutated description for fingerprint test",
                "inputSchema": {"type": "object", "properties": {}, "required": []},
            },
            writes=not original.writes,
        )
        _REGISTRY[name] = mutated
        try:
            assert get_tool_catalog(_REMOTE)["fingerprint"] == before_remote
            assert get_tool_catalog(_LOCAL)["fingerprint"] != before_local
        finally:
            _REGISTRY[name] = original
        assert get_tool_catalog(_LOCAL)["fingerprint"] == before_local

    def test_local_and_remote_fingerprints_differ(self):
        assert get_tool_catalog(_LOCAL)["fingerprint"] != get_tool_catalog(_REMOTE)["fingerprint"]


class TestToolSearchEdge:
    def test_snapshot_mutation_does_not_leak_into_registry_or_next_snapshot(self):
        snapshot = get_tool_catalog(_LOCAL)
        entry = next(e for e in snapshot["tools"] if e["name"] == "ontology_manifest")
        entry["inputSchema"]["properties"]["injected"] = {"type": "string"}
        entry["description"] = "tampered"
        assert "injected" not in (
            _REGISTRY["ontology_manifest"].schema["inputSchema"]["properties"]
        )
        fresh = get_tool_catalog(_LOCAL)
        fresh_entry = next(e for e in fresh["tools"] if e["name"] == "ontology_manifest")
        assert "injected" not in fresh_entry["inputSchema"]["properties"]
        assert fresh_entry["description"] != "tampered"
        assert fresh["fingerprint"] == snapshot["fingerprint"]

    def test_tool_search_is_visible_to_both_principals_and_finds_itself(self):
        for principal in (_LOCAL, _REMOTE):
            with principal_scope(principal):
                listed = {t["name"] for t in tools_for_principal(current_principal())}
            assert "tool_search" in listed
            assert "tool_search" in {t["name"] for t in _search(principal)["tools"]}

    def test_tool_search_is_importable_from_the_package(self):
        from opencrab.mcp.tools import tool_search  # noqa: F401

    def test_tier_pinned_read_and_writes_false(self):
        spec = _REGISTRY["tool_search"]
        assert spec.access is AccessTier.READ
        assert spec.writes is False

    def test_limit_truncates_and_reports_total(self):
        result = _search(_LOCAL, limit=1)
        assert result["returned"] == len(result["tools"]) == 1
        assert result["total_matched"] > 1
