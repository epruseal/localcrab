"""
Action Registry — loads and validates Action Schema YAMLs.

Action schemas live in opencrab/schemas/actions/*.yaml.
Schema is optional: if no file exists for an action, validation always passes.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Any

from opencrab.schemas.loader import load_yaml_schema

ACTIONS_DIR = Path(__file__).parent.parent / "schemas" / "actions"


@cache
def load_action_schema(action_name: str) -> dict[str, Any] | None:
    """
    Load the YAML schema for *action_name* from schemas/actions/<action_name>.yaml.

    Returns None if no schema file exists (action is unregistered — still allowed).
    Result is cached after first load.
    """
    return load_yaml_schema(ACTIONS_DIR, action_name)


def reload_action_schema(action_name: str) -> dict[str, Any] | None:
    """Clear the entire schema cache and reload *action_name* from disk.

    Mirrors ``opencrab.schemas.loader.reload_schema``: ``functools.cache``
    has no per-key eviction, so this clears ALL cached actions (not just
    *action_name*) before reloading.
    """
    load_action_schema.cache_clear()
    return load_action_schema(action_name)


def list_registered_actions() -> list[str]:
    """Return all action names that have a registered YAML schema."""
    if not ACTIONS_DIR.exists():
        return []
    return sorted(p.stem for p in ACTIONS_DIR.glob("*.yaml"))


def validate_action_params(
    action_name: str,
    params: dict[str, Any],
) -> tuple[bool, str | None]:
    """
    Validate *params* against the action's schema.

    Returns
    -------
    (is_valid, error_message)
        error_message is None when valid.
        Schema-optional: if no schema exists, always returns (True, None).
    """
    schema = load_action_schema(action_name)
    if schema is None:
        return True, None

    errors: list[str] = []
    for field, spec in schema.get("parameters", {}).items():
        if spec.get("required", False) and field not in params:
            errors.append(f"Required param '{field}' is missing.")

    if errors:
        return False, "; ".join(errors)
    return True, None


def describe_action(action_name: str) -> dict[str, Any] | None:
    """Return the full schema dict for an action, or None if unregistered."""
    return load_action_schema(action_name)
