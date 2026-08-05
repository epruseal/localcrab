"""
Snapshot contract test for the MCP tool surface (opencrab/mcp/tools/).

This pins the exact set of tool names AND their order in ``TOOLS`` as of the
R9 package-decomposition (Stage 7). It must stay green for the ENTIRE
G-agent migration that follows: as handlers move out of
``opencrab/mcp/tools/__init__.py`` into query.py / pack.py / schema.py /
graph.py / harness.py and get re-registered via ``_registry.tool(...)``,
this file proves TOOLS/dispatch_tool/UnknownToolError are unchanged from the
caller's point of view. It asserts against whatever ``opencrab.mcp.tools``
currently exports — legacy implementation today, registry-built tomorrow —
never against internals of either.
"""

from __future__ import annotations

import pytest

from opencrab.mcp.tools import TOOLS, UnknownToolError, dispatch_tool

# Golden contract: exact tool names, in exact order, snapshotted from the
# pre-migration TOOLS list. Any G-agent commit that changes this list
# (renames, reorders, adds, or removes a tool) must fail this test loudly.
GOLDEN_TOOL_NAMES = [
    "ontology_manifest",
    "ontology_add_node",
    "ontology_add_edge",
    "ontology_query",
    "ontology_get_node",
    "ontology_list_nodes",
    "ontology_list_edges",
    "ontology_impact",
    "ontology_lever_simulate",
    "content_pack_list",
    "schema_pack_list",
    "schema_pack_install",
    "schema_pack_uninstall",
    "pack_create",
    "pack_ingest",
    "harness_promotion_apply",
]


class TestToolsSnapshot:
    def test_tool_names_and_order_match_golden_snapshot(self):
        assert [t["name"] for t in TOOLS] == GOLDEN_TOOL_NAMES

    def test_tool_count_matches_golden_snapshot(self):
        assert len(TOOLS) == len(GOLDEN_TOOL_NAMES)

    def test_no_duplicate_tool_names(self):
        names = [t["name"] for t in TOOLS]
        assert len(names) == len(set(names))


class TestToolSchemaShape:
    @pytest.mark.parametrize("name", GOLDEN_TOOL_NAMES)
    def test_each_tool_has_name_description_input_schema(self, name):
        entry = next(t for t in TOOLS if t["name"] == name)
        assert entry["name"] == name
        assert isinstance(entry.get("description"), str) and entry["description"]
        schema = entry.get("inputSchema")
        assert isinstance(schema, dict)
        assert schema.get("type") == "object"
        assert "properties" in schema
        assert "required" in schema
        assert isinstance(schema["required"], list)
        # Every required field must actually be declared in properties.
        for field in schema["required"]:
            assert field in schema["properties"], (
                f"{name}: required field {field!r} missing from properties"
            )


class TestDispatchContract:
    def test_dispatch_known_tool_ontology_manifest(self):
        # ontology_manifest() takes no ctx/store dependencies — safe to call
        # directly, exercising the real dispatch path end-to-end.
        result = dispatch_tool("ontology_manifest", {})
        assert isinstance(result, dict)

    def test_dispatch_unknown_tool_raises_unknown_tool_error(self):
        with pytest.raises(UnknownToolError):
            dispatch_tool("this_tool_does_not_exist", {})

    def test_dispatch_unknown_tool_is_a_key_error(self):
        # UnknownToolError must stay a KeyError subclass so existing
        # `except KeyError` / `pytest.raises(KeyError)` call sites work.
        with pytest.raises(KeyError):
            dispatch_tool("this_tool_does_not_exist", {})

    def test_dispatch_unknown_tool_message_lists_available_tools(self):
        with pytest.raises(UnknownToolError) as exc_info:
            dispatch_tool("this_tool_does_not_exist", {})
        message = str(exc_info.value)
        assert "Unknown tool: 'this_tool_does_not_exist'" in message
        assert "Available:" in message
        for name in GOLDEN_TOOL_NAMES:
            assert name in message

    def test_dispatch_empty_arguments_on_no_arg_tool(self):
        # ontology_manifest and schema_pack_list take no required arguments;
        # dispatching with an empty arguments dict must not raise a
        # TypeError (missing-argument) or UnknownToolError.
        result = dispatch_tool("ontology_manifest", {})
        assert isinstance(result, dict)


class TestWriteLockCoverage:
    """Issue #65: WRITE_TOOLS used to be a hand-copied set in __init__.py that
    silently drifted from what handlers actually did — ontology_impact and
    ontology_lever_simulate call save_impact()/save_simulation() (SQL INSERTs)
    but were absent from the set, so dispatch_tool never locked around them.

    WRITE_TOOLS is now *derived* from each handler's `@tool(..., writes=True)`
    declaration (see opencrab/mcp/tools/_registry.py). These tests pin that
    invariant so a future write handler that forgets `writes=True` fails here
    instead of silently bypassing the cross-process write lock.
    """

    # Tools known (by inspection of their store calls) to mutate state.
    # Mirrors GOLDEN_TOOL_NAMES's role above: an explicit, reviewable list that
    # must equal what the registry actually declares.
    GOLDEN_WRITE_TOOL_NAMES = {
        "ontology_add_node",
        "ontology_add_edge",
        "ontology_impact",
        "ontology_lever_simulate",
        "schema_pack_install",
        "schema_pack_uninstall",
        "pack_create",
        "pack_ingest",
        "harness_promotion_apply",
    }

    def test_write_tools_matches_golden_list(self):
        from opencrab.mcp.tools import WRITE_TOOLS

        assert WRITE_TOOLS == self.GOLDEN_WRITE_TOOL_NAMES

    def test_write_tools_is_derived_from_registry_writes_flag(self):
        """WRITE_TOOLS must equal {name for name, spec in _REGISTRY if spec.writes} —
        i.e. it is computed, not a second hand-maintained copy."""
        from opencrab.mcp.tools import WRITE_TOOLS
        from opencrab.mcp.tools._registry import _REGISTRY

        declared_writers = {name for name, spec in _REGISTRY.items() if spec.writes}
        assert WRITE_TOOLS == declared_writers

    def test_ontology_impact_and_lever_simulate_declared_as_writes(self):
        """Regression pin for the exact #65 omission."""
        from opencrab.mcp.tools._registry import _REGISTRY

        assert _REGISTRY["ontology_impact"].writes is True
        assert _REGISTRY["ontology_lever_simulate"].writes is True

    def test_dispatch_tool_holds_write_lock_iff_tool_declares_writes(self):
        """Behavioral: dispatch_tool must hold _write_lock while calling any
        handler registered with writes=True, and must NOT hold it otherwise.
        Uses throwaway probe tools (same pattern as
        TestRegistryDuplicateGuard) so this doesn't depend on any specific
        handler's store/context dependencies — it exercises dispatch_tool's
        actual locking behavior directly, which is what would catch a future
        write tool that forgets `writes=True`.
        """
        from unittest.mock import patch

        from opencrab.mcp.tools import dispatch_tool
        from opencrab.mcp.tools._registry import _REGISTRY, tool

        events: list[str] = []

        class _FakeLock:
            def __enter__(self):
                events.append("enter")

            def __exit__(self, *exc_info):
                events.append("exit")

        schema = {
            "description": "probe",
            "inputSchema": {"type": "object", "properties": {}, "required": []},
        }
        write_name = "__contract_test_write_probe__"
        read_name = "__contract_test_read_probe__"
        assert write_name not in _REGISTRY
        assert read_name not in _REGISTRY

        try:

            @tool(write_name, schema, writes=True)
            def _write_probe(**kwargs):
                return {"locked": events == ["enter"]}

            @tool(read_name, schema)
            def _read_probe(**kwargs):
                return {"locked": events == ["enter"]}

            with patch("opencrab.mcp.tools._write_lock", return_value=_FakeLock()):
                result = dispatch_tool(write_name, {})
                assert events == ["enter", "exit"]
                assert result["locked"] is True

                events.clear()
                result = dispatch_tool(read_name, {})
                assert events == []
                assert result["locked"] is False
        finally:
            _REGISTRY.pop(write_name, None)
            _REGISTRY.pop(read_name, None)


class TestRegistryDuplicateGuard:
    def test_tool_decorator_raises_on_duplicate_registration(self):
        from opencrab.mcp.tools._registry import _REGISTRY, tool

        name = "__contract_test_duplicate_probe__"
        assert name not in _REGISTRY
        try:
            schema = {
                "description": "probe",
                "inputSchema": {"type": "object", "properties": {}, "required": []},
            }

            @tool(name, schema)
            def _probe_one():
                return {"ok": True}

            with pytest.raises(ValueError, match="already registered"):

                @tool(name, schema)
                def _probe_two():
                    return {"ok": True}
        finally:
            _REGISTRY.pop(name, None)
