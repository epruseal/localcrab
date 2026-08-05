"""
Tests for opencrab.grammar.validator.validate_node_properties type checking.

Regression coverage for issue #48: the validator only checked required-
presence and enum membership; it never read a property schema's `type`
field, so e.g. Claim.confidence (declared `type: float`) silently accepted
strings. See opencrab/grammar/validator.py's `_PROPERTY_TYPE_MAP` and
`_value_matches_type`.
"""

from __future__ import annotations

import pytest
import yaml

from opencrab.grammar.validator import validate_node_properties
from opencrab.schemas import loader as schema_loader


@pytest.fixture
def tmp_schema_env(tmp_path, monkeypatch):
    types_dir = tmp_path / "types"
    types_dir.mkdir()
    monkeypatch.setattr(schema_loader, "SCHEMAS_DIR", types_dir)
    schema_loader.load_type_schema.cache_clear()
    yield types_dir
    schema_loader.load_type_schema.cache_clear()


def _write_schema(types_dir, node_type: str, prop_type: str) -> None:
    schema = {
        "type": node_type,
        "version": "1.0",
        "space": "concept",
        "properties": {
            "value": {"type": prop_type, "required": False, "nullable": True},
        },
    }
    (types_dir / f"{node_type}.yaml").write_text(yaml.safe_dump(schema), encoding="utf-8")


# Every property type name actually used in opencrab/schemas/types/*.yaml,
# mapped to one valid and one invalid value.
TYPE_CASES = [
    ("string", "hello", 42),
    ("int", 7, "not an int"),
    ("float", 3.14, "not a float"),
]


@pytest.mark.parametrize("type_name,valid_value,invalid_value", TYPE_CASES)
def test_valid_value_passes(tmp_schema_env, type_name, valid_value, invalid_value):
    _write_schema(tmp_schema_env, "Thing", type_name)
    result = validate_node_properties("Thing", {"value": valid_value})
    assert result.valid is True


@pytest.mark.parametrize("type_name,valid_value,invalid_value", TYPE_CASES)
def test_invalid_value_rejected(tmp_schema_env, type_name, valid_value, invalid_value):
    _write_schema(tmp_schema_env, "Thing", type_name)
    result = validate_node_properties("Thing", {"value": invalid_value})
    assert result.valid is False
    assert "value" in result.error
    assert type_name in result.error


def test_float_accepts_plain_int(tmp_schema_env):
    """An int is a valid value for a `float`-typed property (5 == 5.0)."""
    _write_schema(tmp_schema_env, "Thing", "float")
    result = validate_node_properties("Thing", {"value": 5})
    assert result.valid is True


@pytest.mark.parametrize("type_name", ["int", "float"])
def test_bool_rejected_for_int_and_float(tmp_schema_env, type_name):
    """bool is a subclass of int in Python but must not silently pass
    where an int/float is declared."""
    _write_schema(tmp_schema_env, "Thing", type_name)
    result = validate_node_properties("Thing", {"value": True})
    assert result.valid is False
    assert "value" in result.error


def test_unknown_declared_type_does_not_block_ingestion_but_warns(tmp_schema_env, caplog):
    """A declared type this validator doesn't recognise must fail open,
    not closed -- otherwise any schema using a new/unmapped type name
    would break ingestion entirely. But it must not fail *silently*:
    a warning is the signal that the declared constraint is a no-op,
    so whoever declares `type: list` next notices instead of believing
    they added a check that does nothing (this is the exact shape of
    issue #48)."""
    _write_schema(tmp_schema_env, "Thing", "timestamp")
    with caplog.at_level("WARNING", logger="opencrab.grammar.validator"):
        result = validate_node_properties("Thing", {"value": "2026-08-05T00:00:00Z"})
    assert result.valid is True
    assert any(
        "timestamp" in rec.message and "not validated" in rec.message
        for rec in caplog.records
    )


def test_none_value_skips_type_check_when_nullable(tmp_schema_env):
    _write_schema(tmp_schema_env, "Thing", "int")
    result = validate_node_properties("Thing", {"value": None})
    assert result.valid is True


def test_reproduces_issue_48_claim_confidence(tmp_schema_env):
    """Exact repro from the issue: a Korean string fed into a
    `type: float` field must now be rejected, not silently stored."""
    schema = {
        "type": "Claim",
        "version": "1.0",
        "space": "claim",
        "properties": {
            "statement": {"type": "string", "required": True, "nullable": False},
            "confidence": {"type": "float", "required": False, "nullable": True},
        },
    }
    (tmp_schema_env / "Claim.yaml").write_text(yaml.safe_dump(schema), encoding="utf-8")

    result = validate_node_properties(
        "Claim", {"statement": "x", "confidence": "매우 높음"}
    )
    assert result.valid is False
    assert "confidence" in result.error
