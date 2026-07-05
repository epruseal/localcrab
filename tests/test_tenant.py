"""
Contract tests for opencrab/ontology/tenant.py — pure functions, no I/O.

No production callers exist yet for stamp_properties/filter_by_tenant/
extract_tenant_context (grepped: only opencrab/ontology/tenant.py itself
defines them); this is early-stage multi-tenant scaffolding per its module
docstring. Tests pin the documented contract of each function directly.
"""

from __future__ import annotations

import pytest

from opencrab.ontology.tenant import (
    TenantContext,
    extract_tenant_context,
    filter_by_tenant,
    stamp_properties,
)

# ---------------------------------------------------------------------------
# Normal
# ---------------------------------------------------------------------------


class TestTenantContextNormal:
    def test_to_dict_from_dict_roundtrip(self):
        ctx = TenantContext(
            tenant_id="acme", subject_id="user_42", allowed_spaces=["subject", "org"]
        )
        data = ctx.to_dict()
        restored = TenantContext.from_dict(data)
        assert restored == ctx

    def test_to_dict_shape(self):
        ctx = TenantContext(tenant_id="acme", subject_id="u1")
        assert ctx.to_dict() == {
            "tenant_id": "acme",
            "subject_id": "u1",
            "allowed_spaces": None,
        }

    def test_default_factory(self):
        ctx = TenantContext.default()
        assert ctx.tenant_id == "default"
        assert ctx.subject_id is None

    def test_from_dict_defaults_when_keys_absent(self):
        ctx = TenantContext.from_dict({})
        assert ctx == TenantContext(tenant_id="default", subject_id=None, allowed_spaces=None)


class TestExtractTenantContextNormal:
    def test_explicit_kwargs_take_priority_over_headers(self):
        ctx = extract_tenant_context(
            headers={"X-Tenant-Id": "from-header", "X-Subject-Id": "sub-header"},
            tenant_id="from-kwarg",
            subject_id="sub-kwarg",
        )
        assert ctx.tenant_id == "from-kwarg"
        assert ctx.subject_id == "sub-kwarg"

    def test_headers_used_when_no_explicit_kwargs(self):
        ctx = extract_tenant_context(
            headers={"X-Tenant-Id": "acme", "X-Subject-Id": "u1"}
        )
        assert ctx.tenant_id == "acme"
        assert ctx.subject_id == "u1"

    def test_lowercase_header_keys_accepted(self):
        ctx = extract_tenant_context(headers={"x-tenant-id": "acme", "x-subject-id": "u1"})
        assert ctx.tenant_id == "acme"
        assert ctx.subject_id == "u1"

    def test_uppercase_header_preferred_over_lowercase_when_both_present(self):
        ctx = extract_tenant_context(
            headers={"X-Tenant-Id": "canonical", "x-tenant-id": "lowercase"}
        )
        assert ctx.tenant_id == "canonical"

    def test_defaults_to_default_tenant_when_nothing_given(self):
        ctx = extract_tenant_context()
        assert ctx.tenant_id == "default"
        assert ctx.subject_id is None

    def test_no_headers_falls_back_to_default(self):
        ctx = extract_tenant_context(headers=None, tenant_id=None, subject_id=None)
        assert ctx.tenant_id == "default"


class TestStampPropertiesNormal:
    def test_stamps_tenant_id_when_absent(self):
        result = stamp_properties({"name": "n1"}, TenantContext(tenant_id="acme"))
        assert result["tenant_id"] == "acme"
        assert result["name"] == "n1"

    def test_stamps_created_by_when_subject_id_present(self):
        ctx = TenantContext(tenant_id="acme", subject_id="user_42")
        result = stamp_properties({}, ctx)
        assert result["created_by"] == "user_42"

    def test_no_created_by_when_subject_id_absent(self):
        ctx = TenantContext(tenant_id="acme", subject_id=None)
        result = stamp_properties({}, ctx)
        assert "created_by" not in result

    def test_does_not_mutate_input_properties(self):
        original = {"name": "n1"}
        stamp_properties(original, TenantContext(tenant_id="acme"))
        assert original == {"name": "n1"}


class TestFilterByTenantNormal:
    def test_default_tenant_passes_everything_through(self):
        nodes = [
            {"properties": {"tenant_id": "acme"}},
            {"properties": {"tenant_id": "other"}},
            {"properties": {}},
        ]
        result = filter_by_tenant(nodes, TenantContext(tenant_id="default"))
        assert result == nodes

    def test_named_tenant_filters_to_matching_nodes(self):
        nodes = [
            {"properties": {"tenant_id": "acme"}, "id": "a"},
            {"properties": {"tenant_id": "other"}, "id": "b"},
        ]
        result = filter_by_tenant(nodes, TenantContext(tenant_id="acme"))
        assert [n["id"] for n in result] == ["a"]

    def test_node_without_tenant_id_treated_as_default_and_excluded_for_named_tenant(self):
        nodes = [{"properties": {}, "id": "legacy"}]
        result = filter_by_tenant(nodes, TenantContext(tenant_id="acme"))
        assert result == []

    def test_node_without_properties_key_treated_as_default(self):
        nodes = [{"id": "no-props"}]
        result = filter_by_tenant(nodes, TenantContext(tenant_id="acme"))
        assert result == []


# ---------------------------------------------------------------------------
# Error — malformed headers, non-dict properties
# ---------------------------------------------------------------------------


class TestTenantErrors:
    def test_empty_string_header_value_falls_back_to_default(self):
        # An empty string is falsy, so `not tid` treats it the same as
        # missing and falls through to the "default" fallback.
        ctx = extract_tenant_context(headers={"X-Tenant-Id": ""})
        assert ctx.tenant_id == "default"

    def test_empty_headers_dict_falls_back_to_default(self):
        ctx = extract_tenant_context(headers={})
        assert ctx.tenant_id == "default"

    def test_stamp_properties_non_dict_raises_type_error(self):
        # properties is typed dict[str, Any]; a non-dict value violates the
        # contract and surfaces immediately via `{**properties}` rather than
        # silently producing a malformed result.
        with pytest.raises(TypeError):
            stamp_properties(None, TenantContext(tenant_id="acme"))  # type: ignore[arg-type]

    def test_stamp_properties_non_mapping_sequence_raises_type_error(self):
        with pytest.raises(TypeError):
            stamp_properties(["not", "a", "dict"], TenantContext())  # type: ignore[arg-type]

    def test_filter_by_tenant_non_dict_properties_value_treated_as_default(self):
        # properties is annotated dict[str, Any] but filter_by_tenant guards
        # with `node.get("properties") or {}`, so a falsy non-dict (None)
        # degrades to the empty-dict / default-tenant path instead of
        # raising on .get().
        nodes = [{"properties": None, "id": "n1"}]
        result = filter_by_tenant(nodes, TenantContext(tenant_id="acme"))
        assert result == []


# ---------------------------------------------------------------------------
# Edge — empty context, unicode tenant ids, double-stamping
# ---------------------------------------------------------------------------


class TestTenantEdgeCases:
    def test_stamp_properties_idempotent_double_stamping_preserves_first_value(self):
        ctx1 = TenantContext(tenant_id="acme")
        once = stamp_properties({"name": "n1"}, ctx1)

        ctx2 = TenantContext(tenant_id="other-tenant")
        twice = stamp_properties(once, ctx2)

        assert twice["tenant_id"] == "acme"  # first stamp wins, not overwritten

    def test_stamp_properties_empty_properties_dict(self):
        result = stamp_properties({}, TenantContext(tenant_id="acme"))
        assert result == {"tenant_id": "acme"}

    def test_unicode_tenant_id_roundtrips(self):
        ctx = TenantContext(tenant_id="고객-회사", subject_id="사용자")
        restored = TenantContext.from_dict(ctx.to_dict())
        assert restored == ctx

    def test_unicode_tenant_id_filters_correctly(self):
        ctx = TenantContext(tenant_id="테넬트-A")
        nodes = [
            {"properties": {"tenant_id": "테넬트-A"}, "id": "match"},
            {"properties": {"tenant_id": "테넬트-B"}, "id": "nomatch"},
        ]
        result = filter_by_tenant(nodes, ctx)
        assert [n["id"] for n in result] == ["match"]

    def test_filter_by_tenant_empty_node_list(self):
        assert filter_by_tenant([], TenantContext(tenant_id="acme")) == []

    def test_filter_by_tenant_empty_node_list_default_tenant(self):
        assert filter_by_tenant([], TenantContext(tenant_id="default")) == []

    def test_extract_tenant_context_empty_string_subject_and_tenant_kwargs_use_headers(self):
        # Empty-string explicit kwargs are falsy, so headers still apply —
        # mirrors the empty-header-value case but from the kwarg side.
        ctx = extract_tenant_context(
            headers={"X-Tenant-Id": "acme"}, tenant_id="", subject_id=""
        )
        assert ctx.tenant_id == "acme"
