"""
Tests for `nullable` enforcement in grammar.validator.validate_node_properties.

Regression coverage for issue #49 (an explicit ``None`` on a required + enum
field passed both the required check and the enum check) and issue #106
(`nullable` was declared in every schema but read nowhere).

Contract pinned here:

- ``required`` decides whether the KEY must be present.
- ``nullable`` decides whether the VALUE may be ``None``. When a property
  spec does not declare it, it is derived as ``not required``.
- A ``None`` on a non-nullable field is one error for that field. The enum
  and type checks do not add a second error for the same ``None``.
- A ``None`` on a nullable field skips the enum and type checks, as before.
"""

from __future__ import annotations

import pytest
import yaml

from opencrab.grammar.validator import validate_node_properties
from opencrab.schemas import loader as schema_loader
from opencrab.schemas import pack_registry

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_schema_env(tmp_path, monkeypatch):
    types_dir = tmp_path / "types"
    types_dir.mkdir()
    monkeypatch.setattr(schema_loader, "SCHEMAS_DIR", types_dir)
    schema_loader.load_type_schema.cache_clear()
    yield types_dir
    schema_loader.load_type_schema.cache_clear()


@pytest.fixture
def real_schema_env():
    """The repository's own schemas/types/*.yaml, with a clean cache."""
    schema_loader.load_type_schema.cache_clear()
    yield schema_loader.SCHEMAS_DIR
    schema_loader.load_type_schema.cache_clear()


def _write(types_dir, node_type: str, properties: dict) -> None:
    schema = {"type": node_type, "version": "1.0", "space": "concept", "properties": properties}
    (types_dir / f"{node_type}.yaml").write_text(yaml.safe_dump(schema), encoding="utf-8")


def _null_errors(result, field: str) -> list[str]:
    """The null-rejection messages that name *field* (0 or 1 expected)."""
    parts = (result.error or "").split("; ")
    return [p for p in parts if p.startswith(f"Field '{field}'") and "null" in p]


# ---------------------------------------------------------------------------
# 1. Normal: nullable true, valid values, optional absence
# ---------------------------------------------------------------------------


def test_normal_none_on_explicit_nullable_true_passes(tmp_schema_env):
    _write(tmp_schema_env, "T", {"x": {"type": "string", "required": False, "nullable": True}})
    assert validate_node_properties("T", {"x": None}).valid is True


def test_normal_valid_value_on_non_nullable_passes(tmp_schema_env):
    _write(tmp_schema_env, "T", {
        "x": {"type": "string", "required": True, "nullable": False, "enum": ["a", "b"]},
    })
    assert validate_node_properties("T", {"x": "a"}).valid is True


def test_normal_optional_key_absent_passes(tmp_schema_env):
    _write(tmp_schema_env, "T", {"x": {"type": "string", "required": False, "nullable": False}})
    assert validate_node_properties("T", {}).valid is True


# ---------------------------------------------------------------------------
# 2. Error: None on nullable false; the pre-existing errors are unchanged
# ---------------------------------------------------------------------------


def test_error_none_on_required_non_nullable_enum_is_rejected(tmp_schema_env):
    """The #49 shape: required + nullable false + enum, value None."""
    _write(tmp_schema_env, "T", {
        "x": {"type": "string", "required": True, "nullable": False, "enum": ["a", "b"]},
    })
    result = validate_node_properties("T", {"x": None})
    assert result.valid is False
    assert len(_null_errors(result, "x")) == 1


def test_error_missing_key_still_reports_missing(tmp_schema_env):
    _write(tmp_schema_env, "T", {"x": {"type": "string", "required": True, "nullable": False}})
    result = validate_node_properties("T", {})
    assert result.valid is False
    assert result.error == "Required field 'x' is missing."


def test_error_enum_violation_still_reports_enum(tmp_schema_env):
    _write(tmp_schema_env, "T", {
        "x": {"type": "string", "required": True, "nullable": False, "enum": ["a", "b"]},
    })
    result = validate_node_properties("T", {"x": "zzz"})
    assert result.valid is False
    assert result.error == "Field 'x' must be one of ['a', 'b'], got 'zzz'."


def test_error_type_violation_still_reports_type(tmp_schema_env):
    _write(tmp_schema_env, "T", {"x": {"type": "int", "required": True, "nullable": False}})
    result = validate_node_properties("T", {"x": "7"})
    assert result.valid is False
    assert result.error == "Field 'x' must be of type 'int', got str ('7')."


# ---------------------------------------------------------------------------
# 3. Edge: derived default, required+nullable, default+non-nullable, one error
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("required", "expect_valid"),
    [(True, False), (False, True)],
    ids=["required-derives-non-nullable", "optional-derives-nullable"],
)
def test_edge_nullable_absent_is_derived_from_required(tmp_schema_env, required, expect_valid):
    _write(tmp_schema_env, "T", {"x": {"type": "string", "required": required}})
    assert validate_node_properties("T", {"x": None}).valid is expect_valid


def test_edge_required_and_nullable_true_needs_key_but_allows_none(tmp_schema_env):
    _write(tmp_schema_env, "T", {"x": {"type": "string", "required": True, "nullable": True}})
    assert validate_node_properties("T", {}).valid is False
    assert validate_node_properties("T", {"x": None}).valid is True


def test_edge_default_does_not_excuse_explicit_none(tmp_schema_env):
    """Optional + nullable false + default (the Document.format shape):
    absent key is fine, explicit None is not -- the validator applies no
    defaults, so a None would be stored as None."""
    _write(tmp_schema_env, "T", {
        "x": {"type": "string", "required": False, "nullable": False,
              "enum": ["p", "q"], "default": "p"},
    })
    assert validate_node_properties("T", {}).valid is True
    result = validate_node_properties("T", {"x": None})
    assert result.valid is False
    assert len(_null_errors(result, "x")) == 1


def test_edge_none_produces_exactly_one_error_for_the_field(tmp_schema_env):
    """No enum or type error piles onto the null error."""
    _write(tmp_schema_env, "T", {
        "x": {"type": "int", "required": True, "nullable": False, "enum": [1, 2]},
    })
    result = validate_node_properties("T", {"x": None})
    assert result.valid is False
    assert result.error.count("Field 'x'") == 1
    assert "must be one of" not in result.error
    assert "must be of type" not in result.error


def test_edge_explicit_nullable_wins_over_derivation(tmp_schema_env):
    _write(tmp_schema_env, "T", {
        "opt_strict": {"type": "string", "required": False, "nullable": False},
        "req_loose": {"type": "string", "required": True, "nullable": True},
    })
    assert validate_node_properties("T", {"opt_strict": None, "req_loose": "v"}).valid is False
    assert validate_node_properties("T", {"opt_strict": "v", "req_loose": None}).valid is True


# ---------------------------------------------------------------------------
# 4. Issue reproduction against the repository's own User schema
# ---------------------------------------------------------------------------


def test_issue49_user_role_none_is_rejected(real_schema_env):
    result = validate_node_properties("User", {"name": "x", "email": "x@x.com", "role": None})
    assert result.valid is False
    assert len(_null_errors(result, "role")) == 1


def test_issue49_all_required_none_is_rejected(real_schema_env):
    result = validate_node_properties("User", {"name": None, "email": None, "role": None})
    assert result.valid is False
    for field in ("name", "email", "role"):
        assert len(_null_errors(result, field)) == 1


def test_issue49_valid_user_still_passes(real_schema_env):
    ok = validate_node_properties(
        "User", {"name": "x", "email": "x@x.com", "role": "admin", "org_id": None},
    )
    assert ok.valid is True


# ---------------------------------------------------------------------------
# 5. Issue #106: schemas generated by pack_registry carry no `nullable` key
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_pack_env(tmp_path, monkeypatch):
    packs_dir = tmp_path / "packs"
    types_dir = tmp_path / "types"
    packs_dir.mkdir()
    types_dir.mkdir()
    manifest = {
        "name": "testpack",
        "version": "1.0.0",
        "description": "Test pack for nullable derivation.",
        "types": ["Widget"],
        "spaces": ["concept"],
        "type_specs": {
            "Widget": {"space": "concept", "required": ["name"], "optional": ["description"]},
        },
    }
    (packs_dir / "testpack.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    monkeypatch.setattr(pack_registry, "_PACKS_DIR", packs_dir)
    monkeypatch.setattr(pack_registry, "_TYPES_DIR", types_dir)
    monkeypatch.setattr(schema_loader, "SCHEMAS_DIR", types_dir)
    schema_loader.load_type_schema.cache_clear()
    yield types_dir
    schema_loader.load_type_schema.cache_clear()


def test_issue106_generated_schema_required_none_rejected_optional_none_allowed(tmp_pack_env):
    pack_registry.install_pack("testpack")
    schema_loader.load_type_schema.cache_clear()
    data = yaml.safe_load((tmp_pack_env / "Widget.yaml").read_text(encoding="utf-8"))
    # Precondition: the generated schema still carries no nullable key.
    assert all("nullable" not in spec for spec in data["properties"].values())

    rejected = validate_node_properties("Widget", {"name": None})
    assert rejected.valid is False
    assert len(_null_errors(rejected, "name")) == 1

    allowed = validate_node_properties("Widget", {"name": "w", "description": None})
    assert allowed.valid is True


# ---------------------------------------------------------------------------
# 6. Error ordering of previously-invalid input is unchanged
# ---------------------------------------------------------------------------

_MIXED = {
    "a": {"type": "string", "required": True, "nullable": False},
    "b": {"type": "string", "required": False, "nullable": True, "enum": ["x", "y"]},
    "c": {"type": "int", "required": False, "nullable": True},
    "d": {"type": "string", "required": True, "nullable": False},
}

# Captured from the pre-fix validator with the same schema and input:
# missing errors in schema order, then enum errors in input order, then
# type errors in input order.
_MIXED_BEFORE = (
    "Required field 'a' is missing.; "
    "Field 'b' must be one of ['x', 'y'], got 'bad'.; "
    "Field 'c' must be of type 'int', got str ('x').; "
    "Field 'd' must be of type 'string', got int (7)."
)


def test_ordering_of_preexisting_errors_is_unchanged(tmp_schema_env):
    _write(tmp_schema_env, "Mixed", _MIXED)
    result = validate_node_properties("Mixed", {"c": "x", "b": "bad", "d": 7})
    assert result.error == _MIXED_BEFORE


def test_ordering_null_error_joins_the_enum_pass_in_input_order(tmp_schema_env):
    _write(tmp_schema_env, "Mixed", _MIXED)
    result = validate_node_properties("Mixed", {"c": "x", "d": None, "b": "bad"})
    parts = result.error.split("; ")
    assert parts[0] == "Required field 'a' is missing."
    assert "null" in parts[1] and parts[1].startswith("Field 'd'")
    assert parts[2] == "Field 'b' must be one of ['x', 'y'], got 'bad'."
    assert parts[3] == "Field 'c' must be of type 'int', got str ('x')."
    assert len(parts) == 4


# ---------------------------------------------------------------------------
# 7. Pack file ingestion never hands a None to the validator
# ---------------------------------------------------------------------------


def test_pack_transform_node_never_passes_none_to_validator(real_schema_env):
    """`transform_node` flattens None to "" before add_node (and then applies
    its own enum correction), so the nullable rule does not change pack-file
    ingestion. Only "no None reaches the validator" is pinned here. Whether
    "" or the corrected value should pass is outside this change."""
    from opencrab.pack.normalize import transform_node

    row = {
        "id": "u1", "space": "subject", "node_type": "User",
        "properties": {"name": "n", "email": "n@x.invalid", "role": None, "org_id": None},
    }
    _space, node_type, _id, props = transform_node("somepack", row)
    assert node_type == "User"
    assert all(v is not None for v in props.values())
    result = validate_node_properties(node_type, props)
    assert _null_errors(result, "role") == []
    assert _null_errors(result, "org_id") == []


# ---------------------------------------------------------------------------
# 8. Entry points: builder.add_node and both harness dry-run implementations
# ---------------------------------------------------------------------------

_NULL_ROLE_PACKAGE = {
    "package_id": "pkg-null", "mission_id": "m", "run_id": "r",
    "nodes": [{"space": "subject", "node_type": "User", "node_id": "u1",
               "properties": {"name": "x", "email": "x@x.com", "role": None}}],
    "edges": [],
}


@pytest.fixture
def crabharness_root():
    """Same sys.path juggling as tests/test_crabharness_promotion_billing.py's
    autouse fixture (see its docstring): make the repo root win the top-level
    `crabharness` name for the duration of the test."""
    import sys
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent
    conflicting_entry = str(repo_root / "crabharness")
    removed = {
        name: sys.modules.pop(name)
        for name in list(sys.modules)
        if name == "crabharness" or name.startswith("crabharness.")
    }
    old_path = list(sys.path)
    sys.path[:] = [p for p in sys.path if p != conflicting_entry]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    try:
        yield
    finally:
        sys.path[:] = old_path
        for name in list(sys.modules):
            if name == "crabharness" or name.startswith("crabharness."):
                del sys.modules[name]
        sys.modules.update(removed)


def test_builder_add_node_rejects_none_before_any_store_write(real_schema_env, tmp_path):
    from opencrab.auth import principal_scope
    from opencrab.ontology.builder import OntologyBuilder
    from tests.test_builder_gate import ALICE, _Docs, _Graph, _Vec
    from tests.test_builder_gate import sql as _sql_fixture

    store = _sql_fixture.__wrapped__(tmp_path)
    graph = _Graph()
    builder = OntologyBuilder(graph, _Docs(), store, vec=_Vec())
    with principal_scope(ALICE), pytest.raises(ValueError, match="null"):
        builder.add_node(
            "subject", "User", "u-null",
            properties={"name": "x", "email": "x@x.com", "role": None},
            pack_id="pack-a",
        )
    assert graph.nodes == {}


def test_mcp_harness_dry_run_reports_inner_none(real_schema_env, crabharness_root):
    from opencrab.mcp.tools import harness_promotion_apply

    result = harness_promotion_apply(_NULL_ROLE_PACKAGE, dry_run=True)
    assert result["dry_run"] is True
    assert result["node_receipts"] == []
    assert [e["node_id"] for e in result["errors"]] == ["u1"]
    assert "null" in result["errors"][0]["error"]


def test_crabharness_apply_dry_run_reports_inner_none(real_schema_env, crabharness_root, tmp_path):
    import json

    from crabharness.crabharness.apply import apply_promotion_package

    package_path = tmp_path / "package.json"
    package_path.write_text(json.dumps(_NULL_ROLE_PACKAGE), encoding="utf-8")
    result = apply_promotion_package(str(package_path), dry_run=True)
    assert result["dry_run"] is True
    assert result["node_receipts"] == []
    assert [e["node_id"] for e in result["errors"]] == ["u1"]
    assert "null" in result["errors"][0]["error"]


# ---------------------------------------------------------------------------
# 9. Every property in the repository's schemas follows the rule
# ---------------------------------------------------------------------------


def _resolved_nullable(spec: dict) -> bool:
    return bool(spec.get("nullable", not spec.get("required", False)))


def test_every_repository_schema_property_follows_nullable_rule(real_schema_env):
    """For each declared property: a None is accepted exactly when the
    resolved nullable is true. Fixed as a rule over all schemas, not as a
    count, so adding a schema keeps the check meaningful."""
    checked = 0
    for node_type in schema_loader.list_registered_types():
        schema = schema_loader.load_type_schema(node_type)
        for field, spec in (schema.get("properties") or {}).items():
            result = validate_node_properties(node_type, {field: None})
            got_null_error = bool(_null_errors(result, field))
            assert got_null_error is (not _resolved_nullable(spec)), (node_type, field, spec)
            checked += 1
    assert checked > 0
