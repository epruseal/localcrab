"""
Contract tests for opencrab/ontology/tenant.py — pure functions, no I/O.

#145: filter_by_tenant, extract_tenant_context, and TenantContext.allowed_spaces
were deleted (zero production callers -- the identity they carried is now the
server-derived opencrab.auth Principal instead). Only stamp_properties is
still live, called from opencrab.mcp.tools.graph.ontology_add_node. The
classes that pinned the deleted functions' contracts (TestExtractTenantContextNormal,
TestFilterByTenantNormal, and the extract/filter cases in TestTenantErrors/
TestTenantEdgeCases) are removed along with them.
"""

from __future__ import annotations

import pytest

from opencrab.ontology.tenant import TenantContext, stamp_properties

# ---------------------------------------------------------------------------
# Normal
# ---------------------------------------------------------------------------


class TestTenantContextNormal:
    def test_to_dict_from_dict_roundtrip(self):
        ctx = TenantContext(tenant_id="acme", subject_id="user_42")
        data = ctx.to_dict()
        restored = TenantContext.from_dict(data)
        assert restored == ctx

    def test_to_dict_shape(self):
        ctx = TenantContext(tenant_id="acme", subject_id="u1")
        assert ctx.to_dict() == {
            "tenant_id": "acme",
            "subject_id": "u1",
        }

    def test_default_factory(self):
        ctx = TenantContext.default()
        assert ctx.tenant_id == "default"
        assert ctx.subject_id is None

    def test_from_dict_defaults_when_keys_absent(self):
        ctx = TenantContext.from_dict({})
        assert ctx == TenantContext(tenant_id="default", subject_id=None)


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


# ---------------------------------------------------------------------------
# Error — non-dict properties
# ---------------------------------------------------------------------------


class TestTenantErrors:
    def test_stamp_properties_non_dict_raises_type_error(self):
        # properties is typed dict[str, Any]; a non-dict value violates the
        # contract and surfaces immediately via `{**properties}` rather than
        # silently producing a malformed result.
        with pytest.raises(TypeError):
            stamp_properties(None, TenantContext(tenant_id="acme"))  # type: ignore[arg-type]

    def test_stamp_properties_non_mapping_sequence_raises_type_error(self):
        with pytest.raises(TypeError):
            stamp_properties(["not", "a", "dict"], TenantContext())  # type: ignore[arg-type]


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
