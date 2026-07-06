"""
Type Schema Registry loader.

Loads YAML type schemas from opencrab/schemas/types/ and caches them.
If a node type has no registered schema file, load_type_schema() returns None
and validation is skipped (schema-optional pattern).
"""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Any

import yaml

SCHEMAS_DIR = Path(__file__).parent / "types"


def load_yaml_schema(directory: Path, name: str) -> dict[str, Any] | None:
    """Load ``directory/<name>.yaml``, or return None if it doesn't exist.

    Shared by this module's ``load_type_schema`` and
    ``opencrab.execution.action_registry``'s ``load_action_schema`` -- both
    are otherwise-identical schema-optional @cache YAML loaders that only
    differ in which directory and cache they use.
    """
    path = directory / f"{name}.yaml"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


@cache
def load_type_schema(node_type: str) -> dict[str, Any] | None:
    """
    Load the YAML schema for *node_type* from schemas/types/<node_type>.yaml.

    Returns None if no schema file exists for that type.
    The result is cached after the first load.
    """
    return load_yaml_schema(SCHEMAS_DIR, node_type)


def list_registered_types() -> list[str]:
    """Return a list of all node types that have a registered YAML schema."""
    if not SCHEMAS_DIR.exists():
        return []
    return sorted(p.stem for p in SCHEMAS_DIR.glob("*.yaml"))


def reload_schema(node_type: str) -> dict[str, Any] | None:
    """Clear the entire schema cache and reload *node_type* from disk.

    ``functools.cache`` has no per-key eviction, so this clears ALL cached
    types (not just *node_type*) before reloading. Callers (e.g. pack
    installers) only need "cache is not stale" — they don't rely on other
    types' cache entries surviving this call.
    """
    load_type_schema.cache_clear()
    return load_type_schema(node_type)
