"""
Type Schema Registry loader.

Loads YAML type schemas from opencrab/schemas/types/ and caches them.
If a node type has no registered schema file, load_type_schema() returns None
and validation is skipped (schema-optional pattern).
"""

from __future__ import annotations

import logging
from functools import cache
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

import yaml

logger = logging.getLogger(__name__)

SCHEMAS_DIR = Path(__file__).parent / "types"


def safe_schema_name(name: Any) -> bool:
    """Return True if *name* is safe to use as a single path component (#109).

    ``pathlib``'s ``/`` neither resolves ``..`` nor rejects an absolute
    right-hand operand -- it just concatenates, and the filesystem does the
    normalising at ``write_text``/``unlink``/``exists`` time. So every place
    that joins a caller-supplied name onto a directory has to check the name
    first, or the resulting path can address a file outside that directory.

    This is a deny-list, not a character allow-list: unicode type names work
    today (a pack may legitimately declare a Korean type) and an allow-list
    would break them. Only what an escape actually needs is rejected.

    ``.`` and ``..`` are named explicitly rather than left to the
    ``PurePath.name`` comparison below: ``PurePosixPath(".").name`` is ``""``
    (so ``.`` would be caught) but ``PurePosixPath("..").name`` is ``".."``
    (so ``..`` would NOT be). With today's callers -- all of which append a
    ``.yaml`` suffix -- ``..`` merely produces a harmless ``...yaml`` inside
    the directory, but relying on that is relying on the suffix, not on the
    check. A future suffix-less caller would escape.

    Both path flavours are consulted so that ``a\\b`` -- a perfectly legal
    single filename on POSIX -- is rejected too. This is deliberate rather
    than incidental: the files these names produce are written into the
    repository tree (``opencrab/schemas/types/``) and get checked out on
    other platforms, where a backslash or a drive prefix would separate
    components. Names must be portable path components, so ``a\\b`` and
    ``a:b`` are rejected even on Linux. No shipped pack uses such a name.
    """
    if not isinstance(name, str) or not name:
        return False
    if "\x00" in name:
        return False
    if name in (".", ".."):
        return False
    return PurePosixPath(name).name == name and PureWindowsPath(name).name == name


def resolves_inside(path: Path, directory: Path) -> bool:
    """Return True if *path* is still a direct child of *directory* once resolved.

    The companion to ``safe_schema_name`` for callers that WRITE or DELETE.
    A safe name can still address a file outside the directory when the
    entry is a symlink: a *dangling* link pointing outward reads as
    ``exists() == False``, so an installer treats it as a new file and
    ``write_text`` follows the link and creates the file outside.

    Both sides are resolved, so a package installed behind a symlinked
    directory does not produce a false rejection. ``parent ==`` rather than
    ``is_relative_to``: every caller here addresses a direct child file, and
    a subdirectory layout is not supported (joining ``sub/nested`` raises
    ``FileNotFoundError`` rather than creating anything), so the stricter
    comparison states the actual invariant.

    Deliberately NOT applied on read paths. There, the only way in is name
    injection, which ``safe_schema_name`` already stops, and rejecting
    symlinks would diverge from ``pack_registry.list_packs``, which globs the
    directory and follows links -- a symlinked pack would be listed but then
    report "not found" on install.

    Resolution failures (a symlink loop, a permission error mid-path) count
    as "not inside": the check refuses rather than propagating, since Python
    versions differ in whether ``resolve()`` raises on a loop at all.
    """
    try:
        return path.resolve().parent == directory.resolve()
    except (OSError, RuntimeError, ValueError):
        return False


def load_yaml_schema(directory: Path, name: str) -> dict[str, Any] | None:
    """Load ``directory/<name>.yaml``, or return None if it doesn't exist.

    Shared by this module's ``load_type_schema`` and
    ``opencrab.execution.action_registry``'s ``load_action_schema`` -- both
    are otherwise-identical schema-optional @cache YAML loaders that only
    differ in which directory and cache they use.

    A *name* that is not a safe path component (#109) is treated as "no such
    schema" rather than raising: the schema-optional contract already returns
    None for any unregistered name, and an unsafe name cannot name a schema
    inside *directory*, so None is the truthful answer. Raising here would
    instead change the failure shape of every node write that reaches
    ``grammar.validator.validate_node_properties``.
    """
    if not safe_schema_name(name):
        logger.warning(
            "Refusing to load schema for %r: not a safe path component "
            "(no separators, no '.'/'..', not absolute).",
            name,
        )
        return None
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

    Warns when the loaded schema has a legacy shape (top-level `required`/
    `optional` lists, no `properties` mapping) -- consumers such as
    grammar.validator.validate_node_properties only read `properties`, so a
    legacy schema silently enforces nothing. See opencrab/schemas/pack_registry.py
    (install_pack migration) for the fix path: reinstalling the owning pack
    regenerates the file in the current shape.
    """
    schema = load_yaml_schema(SCHEMAS_DIR, node_type)
    if schema is not None and "properties" not in schema and ("required" in schema or "optional" in schema):
        logger.warning(
            "Type schema '%s' has a legacy shape (required/optional without "
            "properties); required-field and enum checks will silently no-op "
            "for this type. Reinstall the owning schema pack to migrate it.",
            node_type,
        )
    return schema


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
