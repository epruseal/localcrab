"""
Contract tests for opencrab.execution.action_registry.

ACTIONS_DIR is monkeypatched to a tmp directory with fixture YAMLs so these
tests don't depend on (or mutate) the real opencrab/schemas/actions/ files.

load_action_schema is decorated with @cache (functools), so its cache is
cleared before and after every test in this module to keep tests isolated
from each other and from other test modules that may load real schemas.
"""

from __future__ import annotations

import pytest
import yaml

import opencrab.execution.action_registry as action_registry


@pytest.fixture(autouse=True)
def _clear_schema_cache():
    action_registry.load_action_schema.cache_clear()
    yield
    action_registry.load_action_schema.cache_clear()


@pytest.fixture
def actions_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(action_registry, "ACTIONS_DIR", tmp_path)
    return tmp_path


def _write_yaml(directory, name, content):
    (directory / f"{name}.yaml").write_text(yaml.safe_dump(content), encoding="utf-8")


# ---------------------------------------------------------------------------
# Normal
# ---------------------------------------------------------------------------


class TestActionRegistryNormal:
    def test_load_action_schema_returns_parsed_yaml(self, actions_dir):
        schema = {
            "action": "do_thing",
            "parameters": {"target_id": {"type": "string", "required": True}},
        }
        _write_yaml(actions_dir, "do_thing", schema)

        loaded = action_registry.load_action_schema("do_thing")
        assert loaded == schema

    def test_validate_action_params_ok_when_required_present(self, actions_dir):
        _write_yaml(
            actions_dir,
            "do_thing",
            {"parameters": {"target_id": {"type": "string", "required": True}}},
        )

        ok, error = action_registry.validate_action_params("do_thing", {"target_id": "x"})
        assert ok is True
        assert error is None

    def test_list_registered_actions_returns_sorted_stems(self, actions_dir):
        _write_yaml(actions_dir, "zeta_action", {"parameters": {}})
        _write_yaml(actions_dir, "alpha_action", {"parameters": {}})

        assert action_registry.list_registered_actions() == ["alpha_action", "zeta_action"]

    def test_describe_action_returns_full_schema(self, actions_dir):
        schema = {"action": "do_thing", "description": "does a thing", "parameters": {}}
        _write_yaml(actions_dir, "do_thing", schema)

        assert action_registry.describe_action("do_thing") == schema


# ---------------------------------------------------------------------------
# Error
# ---------------------------------------------------------------------------


class TestActionRegistryError:
    def test_validate_action_params_missing_required_field(self, actions_dir):
        _write_yaml(
            actions_dir,
            "do_thing",
            {
                "parameters": {
                    "target_id": {"type": "string", "required": True},
                    "note": {"type": "string", "required": False},
                }
            },
        )

        ok, error = action_registry.validate_action_params("do_thing", {"note": "x"})
        assert ok is False
        assert "target_id" in error

    def test_malformed_yaml_raises(self, actions_dir):
        (actions_dir / "broken.yaml").write_text("key: [unclosed", encoding="utf-8")

        with pytest.raises(yaml.YAMLError):
            action_registry.load_action_schema("broken")


# ---------------------------------------------------------------------------
# Edge
# ---------------------------------------------------------------------------


class TestActionRegistryEdge:
    def test_empty_dir_list_registered_actions_returns_empty(self, actions_dir):
        assert action_registry.list_registered_actions() == []

    def test_missing_actions_dir_list_registered_actions_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(action_registry, "ACTIONS_DIR", tmp_path / "does_not_exist")
        assert action_registry.list_registered_actions() == []

    def test_unregistered_action_schema_is_none_and_always_valid(self, actions_dir):
        assert action_registry.load_action_schema("no_such_action") is None
        ok, error = action_registry.validate_action_params("no_such_action", {"anything": 1})
        assert ok is True
        assert error is None

    def test_schema_result_is_cached_and_not_reloaded_on_file_change(self, actions_dir):
        """
        Characterizes the documented behavior ("Result is cached after first
        load.") -- a file edit after the first load is invisible until the
        process-wide cache is cleared. There is no reload_schema() counterpart
        here (unlike schemas/loader.py). This is intentional documentation of
        current behavior, not an assertion that it is the desired long-term
        contract -- see PR notes for the audit judgment.
        """
        _write_yaml(actions_dir, "do_thing", {"parameters": {"a": {"required": True}}})
        first = action_registry.load_action_schema("do_thing")
        assert first == {"parameters": {"a": {"required": True}}}

        _write_yaml(actions_dir, "do_thing", {"parameters": {"b": {"required": True}}})
        second = action_registry.load_action_schema("do_thing")

        assert second == first  # stale: reflects the state at first load, not the edit

    def test_validate_action_params_ignores_enum_and_type_constraints(self):
        """
        Uses the real shipped schema opencrab/schemas/actions/promote_claim.yaml
        (target_status has `enum: [validated, promoted]`) to characterize that
        validate_action_params only checks presence of required fields -- it
        does not enforce declared enum or type constraints, unlike the
        grammar validator elsewhere in the codebase. Documented gap, not
        fixed here -- see PR notes for the audit judgment.
        """
        ok, error = action_registry.validate_action_params(
            "promote_claim",
            {"claim_id": "c1", "target_status": "not_in_the_enum_at_all"},
        )
        assert ok is True
        assert error is None
