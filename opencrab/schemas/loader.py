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


# Mirrors CPython's ntpath._reserved_chars / _reserved_names / _isreservedname
# (see the "Naming Files, Paths, and Namespaces" rules they cite). Vendored
# rather than called: ntpath.isreserved is 3.13+ and this project supports
# 3.11, and isreserved() takes a whole path (it runs splitroot and splits on
# separators) while what is needed here is the single-component predicate.
# tests/test_schema_pack_path_escape.py pins both sets by exact equality and
# compares against ntpath._isreservedname where that exists, so a drift from
# the original shows up as a failure rather than as silent divergence.
_RESERVED_CHARS = frozenset(
    {chr(i) for i in range(32)} | {'"', "*", ":", "<", ">", "?", "|", "/", "\\"}
)
_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{c}" for c in "123456789\xb9\xb2\xb3"}
    | {f"LPT{c}" for c in "123456789\xb9\xb2\xb3"}
)


def _is_reserved_filename(name: str) -> bool:
    """Return True if *name* is reserved as a filename by Windows.

    Note this says nothing about ``.`` and ``..`` -- CPython's original
    excludes them here, and ``safe_schema_name`` rejects them separately.
    """
    if name[-1:] in (".", " "):
        # Trailing dots and spaces are reserved on Windows. Note this is a
        # property of the FILENAME: callers here append a ".yaml" suffix, so
        # "Foo." becomes "Foo..yaml", which does NOT end in a dot. See
        # safe_schema_name for why it is still refused as a logical name.
        return name not in (".", "..")
    if _RESERVED_CHARS.intersection(name):
        return True
    # A DOS device name is reserved with any extension and with trailing
    # spaces before it ("nul", "NUL.yaml", "nul .txt" all address the device).
    return name.partition(".")[0].rstrip(" ").upper() in _RESERVED_NAMES


def safe_schema_name(name: Any) -> bool:
    """Return True if *name* is safe to use as a single path component (#109).

    ``pathlib``'s ``/`` neither resolves ``..`` nor rejects an absolute
    right-hand operand -- it just concatenates, and the filesystem does the
    normalising at ``write_text``/``unlink``/``exists`` time. So every place
    that joins a caller-supplied name onto a directory has to check the name
    first, or the resulting path can address a file outside that directory.

    This is a deny-list, not a character allow-list: unicode type names work
    today (a pack may legitimately declare a Korean type) and an allow-list
    would break them. Only what stops the name from denoting an ordinary file
    inside the directory is rejected.

    ``.`` and ``..`` are named explicitly rather than left to the
    ``PurePath.name`` comparison below: ``PurePosixPath(".").name`` is ``""``
    (so ``.`` would be caught) but ``PurePosixPath("..").name`` is ``".."``
    (so ``..`` would NOT be). With today's callers -- all of which append a
    ``.yaml`` suffix -- ``..`` merely produces a harmless ``...yaml`` inside
    the directory, but relying on that is relying on the suffix, not on the
    check. A future suffix-less caller would escape.

    Both path flavours are consulted so that ``a\\b`` -- a perfectly legal
    single filename on POSIX -- is rejected too, and ``_is_reserved_filename``
    adds the rest of the Windows rules. This is deliberate rather than
    incidental: the files these names produce are written into the repository
    tree (``opencrab/schemas/types/``) and get checked out on other platforms,
    where a backslash or a drive prefix separates components and ``CON.yaml``
    addresses a device rather than a file. So the name must be a path
    component that denotes a file on either platform, and ``a\\b``, ``a:b``
    and ``CON`` are rejected even on Linux. No shipped pack uses such a name;
    a pack that did would now get a clear refusal.

    The rules are applied to the LOGICAL name, not to ``name + ".yaml"``.
    That distinction only matters for the trailing dot/space rule, and it
    makes this check STRICTER than the filename strictly requires: ``Foo.``
    becomes ``Foo..yaml``, which is a perfectly good filename. Refusing it
    anyway is a conservative choice, not an accident:

    - Every trailing dot/space name other than ``.`` and ``..`` is refused
      because the mirrored Windows rule treats it as reserved. (``.`` and
      ``..`` are excluded from that rule -- CPython excludes them and so does
      this copy -- and are refused by the separate check above.) This is the
      reason that applies to all of them.
    - Some of them ALSO break their own round trip as a pack name.
      ``uninstall_pack`` recognises a generated file by the raw substring
      ``pack: <name>``, and PyYAML quotes a value it cannot leave bare, so
      ``pack: 'Foo '`` and ``pack: '...'`` never match the substring and a
      plain uninstall silently keeps the files install created. This depends
      on whether PyYAML quotes the value, NOT on which character the name
      ends with: ``Foo.`` is emitted bare and does round-trip. Pinned by a
      characterisation test.

    One predicate serves both type names and pack names, so both are refused.
    Relaxing the rule means first fixing that marker check to compare the
    parsed value instead of a substring -- tracked as follow-up work,
    deliberately not bundled into a path-escape fix because it changes what
    the DELETE path considers its own.

    What this does NOT promise: that two accepted names are distinct files.
    A case-insensitive filesystem still collapses ``Foo`` and ``foo``, which
    is a separate concern and not checked here.

    Not promised either: that an accepted name always reaches the filesystem
    successfully. A very long name still raises ``OSError`` from the write --
    a crash, not an escape, and the same before this check existed.
    """
    if not isinstance(name, str) or not name:
        return False
    if name in (".", ".."):
        return False
    if _is_reserved_filename(name):
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

    Deliberately NOT applied on read paths. There, the only way UNTRUSTED
    INPUT gets in is name injection, which ``safe_schema_name`` already stops.
    A symlink planted in the directory is a different thing: planting it needs
    write access to that directory, which is outside this check's threat
    model, and refusing it would diverge from ``pack_registry.list_packs``,
    which globs the directory and follows links -- a symlinked pack would be
    listed and then report "not found" on install. Following such a link on a
    read is therefore a supported, deliberate behaviour, pinned by a test.

    What "refuses" means here, precisely: this returns False when ``resolve()``
    raises, and when the resolved parent differs from the resolved directory.
    It does NOT catch every resolution failure -- ``resolve()`` runs with
    ``strict=False``, and on Python 3.13 a symlink loop resolves to itself
    rather than raising, so a loop inside the directory compares equal and
    passes. That is not an escape; the write simply fails later with
    ``OSError``. The exception branch stays because Python versions differ in
    whether a loop raises at all.
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
            "(no separators, not '.'/'..', not absolute, not reserved as a "
            "filename by Windows).",
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
