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
