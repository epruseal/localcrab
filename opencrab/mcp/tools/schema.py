"""Schema pack tools: list/install/uninstall domain schema packs.

No ``_get_context`` usage here — these delegate straight to
``opencrab.schemas.pack_registry``, so there's no mock.patch namespace
concern like the other handler modules (see graph.py's docstring).
"""

from __future__ import annotations

from typing import Any

from ._registry import AccessTier, tool


@tool(
    "schema_pack_list",
    {
        "description": "List all available schema packs (saas, biomedical, legal) with install status.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    order=10,
    access=AccessTier.READ,
)
def schema_pack_list() -> dict[str, Any]:
    """List all available schema packs with install status."""
    from opencrab.schemas.pack_registry import list_packs

    packs = list_packs()
    return {"total": len(packs), "packs": packs}


@tool(
    "schema_pack_install",
    {
        "description": "Install a domain schema pack by generating type YAML files in schemas/types/. The pack name must be a single path component; anything else reads as pack not found.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Pack name: saas, biomedical, or legal."},
            },
            "required": ["name"],
        },
    },
    order=11,
    # #150: ADMIN, not WRITE -- this mutates the HOST FILESYSTEM (type YAML
    # under schemas/types/) as a process-global change, not a graph write.
    # Withheld from remote (non-local) principals -- see
    # opencrab.mcp.tools._registry.allowed_access_tiers.
    access=AccessTier.ADMIN,
    writes=True,
)
def schema_pack_install(name: str) -> dict[str, Any]:
    """
    Install a schema pack by generating type YAML files.

    Existing user-customised schemas are NOT overwritten.

    Returns ``{"error": ...}`` without writing anything when *name* is not a
    single path component, or when the pack manifest declares a type name
    that is not one (#109) -- see ``opencrab.schemas.pack_registry``.

    Parameters
    ----------
    name:
        Pack name (e.g. 'saas', 'biomedical', 'legal').
    """
    from opencrab.schemas.pack_registry import install_pack

    return install_pack(name)


@tool(
    "schema_pack_uninstall",
    {
        "description": "Remove auto-generated type schemas for a pack. User-customised schemas are kept unless force=true. Nothing outside schemas/types/ is ever removed, and force=true does not change that.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Pack name to uninstall."},
                "force": {"type": "boolean", "description": "Remove even user-customised schemas (default false).", "default": False},
            },
            "required": ["name"],
        },
    },
    order=12,
    # #150: ADMIN -- same host-filesystem rationale as schema_pack_install.
    access=AccessTier.ADMIN,
    writes=True,
)
def schema_pack_uninstall(name: str, force: bool = False) -> dict[str, Any]:
    """
    Remove auto-generated type schemas for a pack.

    User-customised schemas (no pack: header) are kept unless force=True.

    Removal never leaves the type directory (#109): an unsafe pack name reads
    as not found, a manifest with an unsafe type name is refused whole
    without deleting anything, and a path that cannot be shown to resolve
    inside the type directory is kept rather than removed -- ``force=True``
    overrides the user-customised check, not the containment one.
    """
    from opencrab.schemas.pack_registry import uninstall_pack

    return uninstall_pack(name, force)
